import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import installWatch

app = QApplication.instance() or QApplication([])


def installedCopy(folder, version):
    with open(os.path.join(folder, "tray.py"), "w") as f:
        f.write("")
    with open(os.path.join(folder, "version.py"), "w") as f:
        f.write("VERSION = \"" + version + "\"\n")


def waitFor(condition, timeout=2.0):
    for i in range(int(timeout * 50)):
        if (condition()):
            return True
        QTest.qWait(20)
    return condition()


class InstallWatchTests(unittest.TestCase):
    def setUp(self):
        self.watcher = installWatch.InstallWatcher()
        self.removals = []
        self.watcher.removed.connect(lambda: self.removals.append(1))

    def testAFileChangeStartsTheTimer(self):
        with mock.patch.object(installWatch.os, "execv"), tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, installWatch.VERSION)
            with mock.patch.object(installWatch, "APP_FOLDER", folder):
                self.watcher.start()
                self.assertFalse(self.watcher.timer.isActive())
                with open(os.path.join(folder, "version.py"), "w") as f:
                    f.write("VERSION = \"9.9.9\"\n")
                started = waitFor(lambda: self.watcher.timer.isActive())
                self.watcher.timer.stop()
                self.watcher.watcher.removePaths(self.watcher.watcher.files() + self.watcher.watcher.directories())
        self.assertTrue(started)

    def testABrokenTrayPyFailsTheRealImportCheck(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("import notThere\n")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv") as execv, contextlib.redirect_stderr(errors):
                self.watcher.onChanged()
        self.assertFalse(execv.called)
        self.assertEqual(errors.getvalue(), "Version 9.9.9 didn't import, so " + installWatch.VERSION + " keeps running\n")

    def testReadVersion(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(installWatch.readVersion(folder))
            with open(os.path.join(folder, "version.py"), "w") as f:
                f.write("VERSION = \"1.2.3\"\n")
            self.assertEqual(installWatch.readVersion(folder), "1.2.3")

    def testAMissingTrayPyMeansTheAppWasRemoved(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "APP_FOLDER", folder):
            self.watcher.onChanged()
        self.assertEqual(self.removals, [1])

    def testANewVersionThatImportsRestartsTheApp(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, mock.patch.object(installWatch.os, "execv") as execv, contextlib.redirect_stderr(errors):
                self.watcher.onChanged()
        self.assertEqual(run.call_args.args[0], [sys.executable, "-B", "-c", "import tray"])
        self.assertEqual(execv.call_args.args[1], [sys.executable, "-B", os.path.join(folder, "tray.py")])
        self.assertEqual(errors.getvalue(), "")

    def testAFailedExecKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run", return_value=mock.Mock(returncode=0)), mock.patch.object(installWatch.os, "execv", side_effect=OSError(8, "Exec format error")), contextlib.redirect_stderr(errors):
                self.watcher.onChanged()
        self.assertEqual(errors.getvalue(), "Version 9.9.9 couldn't start ([Errno 8] Exec format error), so " + installWatch.VERSION + " keeps running\n")

    def testANewVersionThatDoesntImportKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run", return_value=mock.Mock(returncode=1)), mock.patch.object(installWatch.os, "execv") as execv, contextlib.redirect_stderr(errors):
                self.watcher.onChanged()
        self.assertFalse(execv.called)
        self.assertEqual(errors.getvalue(), "Version 9.9.9 didn't import, so " + installWatch.VERSION + " keeps running\n")

    def testAVersionCheckThatCantFinishKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            for error in (installWatch.subprocess.TimeoutExpired([sys.executable], 60), OSError("exec failed")):
                with self.subTest(error=type(error).__name__):
                    errors = io.StringIO()
                    with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run", side_effect=error), mock.patch.object(installWatch.os, "execv") as execv, contextlib.redirect_stderr(errors):
                        self.watcher.onChanged()
                    self.assertFalse(execv.called)
                    self.assertEqual(errors.getvalue(), "Version 9.9.9 couldn't be checked (" + str(error) + "), so " + installWatch.VERSION + " keeps running\n")

    def testTheSameVersionDoesNothing(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, installWatch.VERSION)
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run") as run:
                self.watcher.onChanged()
        self.assertFalse(run.called)

    def testTheAppImportsTheWayAnOlderVersionChecksIt(self):
        # 1.0.0 runs exactly this in the new app folder before it restarts into it, with no display
        env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "PYTHONPATH")}
        result = subprocess.run([sys.executable, "-B", "-c", "import tray"], cwd=installWatch.APP_FOLDER, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))


if (__name__ == "__main__"):
    unittest.main()
