import os
import re
import sys

from PyQt6.QtCore import QFileSystemWatcher, QObject, QProcess, pyqtSignal

import mice
from version import VERSION

INSTALL_SETTLE_MS = 2 * 1000
CHECK_TIMEOUT_MS = 60 * 1000
# a check can fail while the rest of a large upgrade is still landing, so it runs again after each of these waits
CHECK_RETRY_MS = (30 * 1000, 120 * 1000, 600 * 1000)
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
        self.retryTimer = mice.singleShotTimer(self, CHECK_RETRY_MS[0], self.onChanged)
        self.retries = 0
        self.check = None
        self.checkVersion = None
        self.checkAgain = False
        self.checkTimedOut = False

    def start(self):
        self.watcher.directoryChanged.connect(lambda path: self.onFolderChanged())
        self.watcher.fileChanged.connect(lambda path: self.onFolderChanged())
        self.watcher.addPath(APP_FOLDER)
        self.watcher.addPath(os.path.join(APP_FOLDER, "version.py"))

    def onFolderChanged(self):
        # a new install starts the retries over
        self.retries = 0
        self.retryTimer.stop()
        self.timer.start()

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
            self.endCheck("didn't import", self.lastErrorLine())
        else:
            self.endCheck(None)

    def lastErrorLine(self):
        # a failed import ends its traceback with the exception, which is the line that says why
        text = bytes(self.check.readAllStandardError()).decode("utf-8", "replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1][:200] if lines else None

    def endCheck(self, problem, detail=None):
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
            if (detail is not None):
                print("  " + detail, file=sys.stderr)
            if (self.retries < len(CHECK_RETRY_MS)):
                self.retryTimer.start(CHECK_RETRY_MS[self.retries])
                self.retries += 1
            return
        # the lock file is opened without inheritance (PEP 446), so the exec closes it and the new version takes the lock
        try:
            os.execv(sys.executable, [sys.executable, "-B", os.path.join(APP_FOLDER, "tray.py")])
        except OSError as error:
            print("Version " + newVersion + " couldn't start (" + str(error) + "), so " + VERSION + " keeps running", file=sys.stderr)
