import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
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

    def tearDown(self):
        for timer in (self.watcher.timer, self.watcher.retryTimer, self.watcher.checkTimer):
            timer.stop()

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

    def testReadVersion(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(installWatch.readVersion(folder))
            with open(os.path.join(folder, "version.py"), "w") as f:
                f.write("VERSION = \"1.2.3\"\n")
            self.assertEqual(installWatch.readVersion(folder), "1.2.3")

    def testReadVersionSkipsALineThatOnlyEndsInVersion(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "version.py"), "w") as f:
                f.write("OLD_VERSION = \"0.0.1\"\nVERSION = \"1.2.3\"\n")
            self.assertEqual(installWatch.readVersion(folder), "1.2.3")

    def testAMissingTrayPyMeansTheAppWasRemoved(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "APP_FOLDER", folder):
            self.watcher.onChanged()
        self.assertEqual(self.removals, [1])

    def runCheck(self, folder, trayText, execv=None):
        # the check imports the new tray.py in its own process, so a test writes one that behaves the way it needs
        installedCopy(folder, "9.9.9")
        with open(os.path.join(folder, "tray.py"), "w") as f:
            f.write(trayText)
        errors = io.StringIO()
        execv = execv or mock.Mock()
        with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv", execv), contextlib.redirect_stderr(errors):
            self.watcher.onChanged()
            self.assertTrue(waitFor(lambda: self.watcher.check is None, timeout=10.0))
        return (execv, errors.getvalue())

    def testANewVersionThatImportsRestartsTheApp(self):
        with tempfile.TemporaryDirectory() as folder:
            execv, errors = self.runCheck(folder, "")
        self.assertEqual(execv.call_args.args[1], [sys.executable, "-B", os.path.join(folder, "tray.py")])
        self.assertEqual(errors, "Starting version 9.9.9 in place of " + installWatch.VERSION + "\n")

    def testTheRestartCarriesWhatTheAppAsksToKeep(self):
        self.watcher = installWatch.InstallWatcher(lambda: ["--open-window"])
        with tempfile.TemporaryDirectory() as folder:
            execv, errors = self.runCheck(folder, "")
        self.assertEqual(execv.call_args.args[1], [sys.executable, "-B", os.path.join(folder, "tray.py"), "--open-window"])

    def testABrokenTrayPyFailsTheRealImportCheckAndSaysWhy(self):
        with tempfile.TemporaryDirectory() as folder:
            execv, errors = self.runCheck(folder, "import notThere\n")
            self.watcher.retryTimer.stop()
        self.assertFalse(execv.called)
        self.assertEqual(errors, "Version 9.9.9 didn't import, so " + installWatch.VERSION + " keeps running\n  ModuleNotFoundError: No module named 'notThere'\n")

    def testAFailedCheckIsTriedAgain(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "CHECK_RETRY_MS", (100,)):
            execv, errors = self.runCheck(folder, "import notThere\n")
            # the rest of the install lands after the first check failed
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("")
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv", execv), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(waitFor(lambda: execv.called, timeout=10.0))

    def testAFailingCheckIsTriedAFewTimesThenLeft(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "CHECK_RETRY_MS", (50, 50)), mock.patch.object(self.watcher, "startCheck", wraps=self.watcher.startCheck) as startCheck:
            self.runCheck(folder, "import notThere\n")
            with mock.patch.object(installWatch, "APP_FOLDER", folder), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(waitFor(lambda: startCheck.call_count == 3 and self.watcher.check is None, timeout=10.0))
                QTest.qWait(300)
        self.assertEqual((startCheck.call_count, self.watcher.retryTimer.isActive()), (3, False))

    def testTheLastFailedRetrySaysItIsTheLast(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "CHECK_RETRY_MS", (50,)), mock.patch.object(self.watcher, "startCheck", wraps=self.watcher.startCheck) as startCheck:
            self.runCheck(folder, "import notThere\n")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), contextlib.redirect_stderr(errors):
                self.assertTrue(waitFor(lambda: startCheck.call_count == 2 and self.watcher.check is None, timeout=10.0))
        self.assertEqual(errors.getvalue().splitlines()[-1], "Not checking version 9.9.9 again until the app folder changes")

    def testABrokenStderrDoesNotStopTheRestart(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch.sys, "stderr", mock.Mock(write=mock.Mock(side_effect=OSError(5, "I/O error")))):
            installedCopy(folder, "9.9.9")
            execv = mock.Mock()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv", execv):
                self.watcher.onChanged()
                self.assertTrue(waitFor(lambda: execv.called, timeout=10.0))

    def testAFailedExecKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder:
            execv, errors = self.runCheck(folder, "", execv=mock.Mock(side_effect=OSError(8, "Exec format error")))
        self.assertEqual(errors, "Starting version 9.9.9 in place of " + installWatch.VERSION + "\nVersion 9.9.9 couldn't start ([Errno 8] Exec format error), so " + installWatch.VERSION + " keeps running\n")

    def testACheckThatRunsTooLongIsStoppedAndKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "CHECK_TIMEOUT_MS", 300):
            execv, errors = self.runCheck(folder, "import time\ntime.sleep(30)\n")
        self.assertFalse(execv.called)
        self.assertEqual(errors, "Version 9.9.9 couldn't be checked (the import didn't finish in time), so " + installWatch.VERSION + " keeps running\n")

    def testACheckThatCantStartKeepsRunning(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(installWatch, "PYTHON", os.path.join(folder, "missing")):
            execv, errors = self.runCheck(folder, "")
        self.assertFalse(execv.called)
        self.assertTrue(errors.startswith("Version 9.9.9 couldn't be checked ("), errors)
        self.assertTrue(errors.endswith("), so " + installWatch.VERSION + " keeps running\n"), errors)

    def testTheCheckRunsBesideTheAppInsteadOfHoldingIt(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("import time\ntime.sleep(1)\n")
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv") as execv:
                started = time.monotonic()
                self.watcher.onChanged()
                returned = time.monotonic() - started
                self.assertTrue(waitFor(lambda: execv.called, timeout=10.0))
        self.assertLess(returned, 0.5)

    def testAChangeDuringACheckChecksAgainBeforeRestarting(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("import time\ntime.sleep(0.5)\n")
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv") as execv, mock.patch.object(self.watcher, "startCheck", wraps=self.watcher.startCheck) as startCheck:
                self.watcher.onChanged()
                self.watcher.onChanged()
                self.assertTrue(waitFor(lambda: execv.called, timeout=10.0))
        self.assertEqual((startCheck.call_count, execv.call_count), (2, 1))

    def testAMissingTrayPyDuringACheckStillQuits(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, "9.9.9")
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("import time\ntime.sleep(0.5)\n")
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.os, "execv"), contextlib.redirect_stderr(io.StringIO()):
                self.watcher.onChanged()
                os.remove(os.path.join(folder, "tray.py"))
                self.watcher.onChanged()
                self.assertEqual(self.removals, [1])
                self.assertTrue(waitFor(lambda: self.watcher.check is None, timeout=10.0))

    def testTheSameVersionDoesNothing(self):
        with tempfile.TemporaryDirectory() as folder:
            installedCopy(folder, installWatch.VERSION)
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(self.watcher, "startCheck") as startCheck:
                self.watcher.onChanged()
        self.assertFalse(startCheck.called)

    def testTheAppImportsTheWayAnOlderVersionChecksIt(self):
        # 1.0.0 runs exactly this in the new app folder before it restarts into it, with no display
        env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "PYTHONPATH")}
        result = subprocess.run([sys.executable, "-B", "-c", "import tray"], cwd=installWatch.APP_FOLDER, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))


if (__name__ == "__main__"):
    unittest.main()
