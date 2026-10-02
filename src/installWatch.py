import os
import re
import sys

from PyQt6.QtCore import QFileSystemWatcher, QObject, QProcess, pyqtSignal

import mice
from version import VERSION

INSTALL_SETTLE_MS = 2 * 1000
CHECK_TIMEOUT_MS = 60 * 1000
APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


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
        self.checkTimer = mice.singleShotTimer(self, CHECK_TIMEOUT_MS, self.onCheckTimeout)
        self.check = None
        self.checkVersion = None
        self.checkAgain = False
        self.checkTimedOut = False

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
        if (self.check is not None):
            # the files a running check imported may have changed since, so its answer is set aside and the folder looked at again
            self.checkAgain = True
            return
        newVersion = readVersion(APP_FOLDER)
        if (newVersion is None or newVersion == VERSION):
            return
        self.startCheck(newVersion)

    def startCheck(self, newVersion):
        # importing the new version runs beside the tray instead of in this slot, which would hold the icon and menu still until it ends
        check = QProcess(self)
        check.setWorkingDirectory(APP_FOLDER)
        check.setStandardOutputFile(QProcess.nullDevice())
        check.setStandardErrorFile(QProcess.nullDevice())
        check.finished.connect(self.onCheckFinished)
        check.errorOccurred.connect(self.onCheckError)
        self.check = check
        self.checkVersion = newVersion
        self.checkAgain = False
        self.checkTimedOut = False
        self.checkTimer.start(CHECK_TIMEOUT_MS)
        check.start(PYTHON, ["-B", "-c", "import tray"])

    def onCheckTimeout(self):
        self.checkTimedOut = True
        self.check.kill()

    def onCheckError(self, error):
        # a process that never started sends no finished signal, while every other error is followed by one
        if (error == QProcess.ProcessError.FailedToStart):
            self.endCheck("couldn't be checked (" + self.check.errorString() + ")")

    def onCheckFinished(self, exitCode, exitStatus):
        if (self.checkTimedOut):
            self.endCheck("couldn't be checked (the import didn't finish in time)")
        elif (exitStatus != QProcess.ExitStatus.NormalExit or exitCode != 0):
            self.endCheck("didn't import")
        else:
            self.endCheck(None)

    def endCheck(self, problem):
        newVersion = self.checkVersion
        self.checkTimer.stop()
        self.check.deleteLater()
        self.check = None
        if (self.checkAgain):
            self.checkAgain = False
            self.onChanged()
            return
        if (problem is not None):
            print("Version " + newVersion + " " + problem + ", so " + VERSION + " keeps running", file=sys.stderr)
            return
        # the hidraw fds, the wake pipes, and the lock file are opened without inheritance (PEP 446), so the exec closes them and the new version takes the lock
        try:
            os.execv(sys.executable, [sys.executable, "-B", os.path.join(APP_FOLDER, "tray.py")])
        except OSError as error:
            print("Version " + newVersion + " couldn't start (" + str(error) + "), so " + VERSION + " keeps running", file=sys.stderr)
